import { render, screen, within } from "@testing-library/react";
import { RollCard } from ".";
import type { RollCardProps, RollLabels } from ".";

// Карточка только показывает то, что ей передали: числа приходят с сервера, а слова вокруг них
// подготавливает вызывающий код (в игре — components/RollCard, в превью стиля — он же).

// Системная настройка «меньше движения» для проверки классов анимации.
function stubReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      media: query,
      matches: query === "(prefers-reduced-motion: reduce)" && reduce,
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  );
}

const LABELS: RollLabels = {
  formula: ["12 + 5 + 2"],
  against: "против КД 15",
  outcome: "Попадание",
  rolls: null,
  summary: "Скелет. Атака: Короткий меч: 12 + 5 + 2 = 19 против КД 15 ✓",
};

const ROLL: RollCardProps = {
  actor: "Скелет",
  label: "Атака: Короткий меч",
  natural: 12,
  total: 19,
  success: true,
  labels: LABELS,
};

describe("RollCard", () => {
  test("показывает кто, что, формулу, итог, «против» и исход", () => {
    render(<RollCard {...ROLL} />);

    const card = screen.getByRole("article");
    expect(within(card).getByText("Скелет")).toBeInTheDocument();
    expect(within(card).getByText("Атака: Короткий меч")).toBeInTheDocument();
    expect(within(card).getByText("12 + 5 + 2")).toBeInTheDocument();
    expect(within(card).getByText("19")).toHaveClass("ui-roll__total");
    expect(within(card).getByText("против КД 15")).toBeInTheDocument();
    expect(within(card).getByText("Попадание")).toBeInTheDocument();
  });

  test("число итога выводится как пришло: карточка ничего не складывает", () => {
    render(<RollCard {...ROLL} total={99} />);

    expect(screen.getByText("99")).toHaveClass("ui-roll__total");
    expect(screen.getByRole("article")).not.toHaveTextContent("19");
  });

  test("доступное имя — готовая запись для чтения вслух", () => {
    render(<RollCard {...ROLL} />);

    expect(screen.getByRole("article")).toHaveAccessibleName(LABELS.summary);
  });

  test("формула может состоять из нескольких строк: каждая в своём элементе", () => {
    render(
      <RollCard {...ROLL} labels={{ ...LABELS, formula: ["1к8+3", "5"] }} />,
    );

    expect(screen.getByText("1к8+3")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
  });

  test("цвет метки исхода задаёт success, а слова — подпись", () => {
    const { rerender } = render(<RollCard {...ROLL} />);
    expect(screen.getByText("Попадание")).toHaveClass("ui-badge--cloth");

    rerender(
      <RollCard
        {...ROLL}
        success={false}
        labels={{ ...LABELS, outcome: "Промах" }}
      />,
    );
    expect(screen.getByText("Промах")).toHaveClass("ui-badge--seal");
  });

  test("без «против» и исхода показан только итог", () => {
    render(
      <RollCard
        {...ROLL}
        success={null}
        labels={{ ...LABELS, against: null, outcome: null }}
      />,
    );

    const card = screen.getByRole("article");
    expect(card.querySelector(".ui-badge")).toBeNull();
    expect(within(card).getByText("19")).toBeInTheDocument();
  });

  test("строка «Выпало» — из подписи; без неё строки нет", () => {
    const { rerender } = render(
      <RollCard {...ROLL} labels={{ ...LABELS, rolls: "Выпало: 3, 14" }} />,
    );
    expect(screen.getByText("Выпало: 3, 14")).toBeInTheDocument();

    rerender(<RollCard {...ROLL} labels={LABELS} />);
    expect(screen.queryByText(/^Выпало/)).toBeNull();
  });

  test("преимущество и помеха — метки карточки", () => {
    const { rerender } = render(<RollCard {...ROLL} advantage="advantage" />);
    expect(screen.getByText("Преимущество")).toHaveClass("ui-badge--amber");

    rerender(<RollCard {...ROLL} advantage="disadvantage" />);
    expect(screen.getByText("Помеха")).toBeInTheDocument();
    expect(screen.queryByText("Преимущество")).toBeNull();

    rerender(<RollCard {...ROLL} advantage="none" />);
    expect(screen.queryByText("Помеха")).toBeNull();
    expect(screen.queryByText("Преимущество")).toBeNull();
  });

  test("грань кубика показывает выпавшее на к20, у броска без к20 грани нет", () => {
    const { container, rerender } = render(<RollCard {...ROLL} />);
    expect(container.querySelector(".ui-roll__face")).toHaveTextContent("12");

    rerender(<RollCard {...ROLL} natural={null} />);
    expect(container.querySelector(".ui-roll__face")).toBeNull();
  });

  test("текст выводится буквами, а не разметкой", () => {
    const { container } = render(
      <RollCard
        {...ROLL}
        actor="<img src=x onerror=alert(1)>"
        labels={{ ...LABELS, outcome: "<b>Попадание</b>" }}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container).toHaveTextContent("<b>Попадание</b>");
  });
});

describe("движение кубика", () => {
  test("по умолчанию кубик перекатывается при появлении", () => {
    stubReducedMotion(false);

    const { container } = render(<RollCard {...ROLL} />);

    expect(container.querySelector(".ui-roll__die")).toHaveClass(
      "ui-anim-roll",
    );
  });

  test("animate=false: старая запись ленты стоит спокойно", () => {
    stubReducedMotion(false);

    const { container } = render(<RollCard {...ROLL} animate={false} />);

    expect(container.querySelector('[class*="ui-anim-"]')).toBeNull();
  });

  test("«меньше движения»: классов анимации нет", () => {
    stubReducedMotion(true);

    const { container } = render(<RollCard {...ROLL} />);

    expect(container.querySelector('[class*="ui-anim-"]')).toBeNull();
  });
});
