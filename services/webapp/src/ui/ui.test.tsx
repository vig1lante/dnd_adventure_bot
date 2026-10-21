import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import {
  Alert,
  Avatar,
  Badge,
  Button,
  Card,
  Countdown,
  EmptyState,
  HpBar,
  Icon,
  Loading,
  Panel,
  SealedNote,
} from ".";
import { avatarTone, formatClock, hpTone, initials } from "./format";
import { ICON_NAMES } from "./icon-names";

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

describe("формат", () => {
  test.each([
    [0, "0:00"],
    [5, "0:05"],
    [90, "1:30"],
    [600, "10:00"],
    [3725, "1:02:05"],
    [-4, "0:00"],
    [59.2, "1:00"], // остаток округляется вверх: «0:00» только когда время вышло
    [Number.NaN, "0:00"],
  ])("formatClock(%j) → %s", (seconds, text) => {
    expect(formatClock(seconds)).toBe(text);
  });

  test.each([
    ["Лира", "Л"],
    ["лира", "Л"],
    ["Лира Ветер", "ЛВ"],
    ["  много   лишних   пробелов ", "МЛ"],
    ["dev-3", "D3"],
    ["", "?"],
    ["   ", "?"],
  ])("initials(%j) → %s", (name, letters) => {
    expect(initials(name)).toBe(letters);
  });

  test("цвет аватара зависит только от имени и укладывается в 6 оттенков", () => {
    const tones = ["Лира", "Борин", "Кай", "Эльза", "dev-1", "dev-2", "x"].map(
      avatarTone,
    );

    expect(avatarTone("Лира")).toBe(avatarTone("Лира"));
    for (const tone of tones) {
      expect(Number.isInteger(tone) && tone >= 0 && tone < 6).toBe(true);
    }
    expect(new Set(tones).size).toBeGreaterThan(1);
  });

  test.each([
    [20, 20, "high"],
    [11, 20, "high"],
    [10, 20, "mid"],
    [6, 20, "mid"],
    [5, 20, "low"],
    [0, 20, "low"],
    [3, 0, "low"],
  ] as const)("hpTone(%j из %j) → %s", (current, max, tone) => {
    expect(hpTone(current, max)).toBe(tone);
  });
});

describe("Icon", () => {
  test("набор иконок плана: d20, свеча, свиток, щит, сердце, песочные часы, печать", () => {
    expect([...ICON_NAMES].sort()).toEqual(
      [
        "candle",
        "d20",
        "heart",
        "hourglass",
        "scroll",
        "seal",
        "shield",
      ].sort(),
    );
  });

  test.each(ICON_NAMES)(
    "%s: встроенный SVG, скрыт от экранных читалок",
    (name) => {
      const { container } = render(<Icon name={name} />);

      const svg = container.querySelector("svg");
      expect(svg).not.toBeNull();
      expect(svg).toHaveAttribute("aria-hidden", "true");
      expect(svg).toHaveAttribute("focusable", "false");
      expect(
        svg?.querySelector("path, circle, rect, ellipse, line, polygon"),
      ).not.toBeNull();
    },
  );

  test("с подписью — картинка с именем", () => {
    render(<Icon name="candle" title="Свеча" />);

    expect(screen.getByRole("img", { name: "Свеча" })).toBeInTheDocument();
  });

  test("размер задаётся числом", () => {
    const { container } = render(<Icon name="d20" size={40} />);

    expect(container.querySelector("svg")).toHaveAttribute("width", "40");
    expect(container.querySelector("svg")).toHaveAttribute("height", "40");
  });
});

describe("Button", () => {
  test("кнопка с подписью; по умолчанию type=button, чтобы не отправлять формы", () => {
    render(<Button>Бросить кубик</Button>);

    const button = screen.getByRole("button", { name: "Бросить кубик" });
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveClass("ui-button", "ui-button--primary");
  });

  test.each(["primary", "secondary", "danger"] as const)(
    "вид %s",
    (variant) => {
      render(<Button variant={variant}>Кнопка</Button>);

      expect(screen.getByRole("button")).toHaveClass(`ui-button--${variant}`);
    },
  );

  test("иконка украшает и не попадает в имя кнопки", () => {
    render(<Button icon="d20">Бросить</Button>);

    expect(screen.getByRole("button", { name: "Бросить" })).toBeInTheDocument();
  });

  test("нажатие вызывает обработчик", () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Ок</Button>);

    fireEvent.click(screen.getByRole("button", { name: "Ок" }));

    expect(onClick).toHaveBeenCalledTimes(1);
  });

  test("busy: кнопка занята — недоступна и сообщает об этом", () => {
    const onClick = vi.fn();
    render(
      <Button busy onClick={onClick}>
        Отправить
      </Button>,
    );

    const button = screen.getByRole("button", { name: "Отправить" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  test("disabled и block", () => {
    render(
      <Button disabled block>
        Нельзя
      </Button>,
    );

    expect(screen.getByRole("button", { name: "Нельзя" })).toBeDisabled();
    expect(screen.getByRole("button")).toHaveClass("ui-button--block");
  });
});

describe("Card и Panel", () => {
  test("Card с заголовком — именованный раздел", () => {
    render(<Card title="Мои игры">Тут пока пусто</Card>);

    const card = screen.getByRole("region", { name: "Мои игры" });
    expect(card).toHaveClass("ui-card");
    expect(
      within(card).getByRole("heading", { level: 2, name: "Мои игры" }),
    ).toBeInTheDocument();
    expect(within(card).getByText("Тут пока пусто")).toBeInTheDocument();
  });

  test("уровень заголовка настраивается", () => {
    render(
      <Card title="Вложенная" headingLevel={3}>
        Текст
      </Card>,
    );

    expect(screen.getByRole("heading", { level: 3 })).toBeInTheDocument();
  });

  test("Card без заголовка — просто блок без раздела", () => {
    render(<Card>Только текст</Card>);

    expect(screen.queryByRole("region")).toBeNull();
    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.getByText("Только текст")).toBeInTheDocument();
  });

  test("Panel: деревянная по умолчанию, суконная по запросу", () => {
    render(
      <>
        <Panel title="Партия">Герои</Panel>
        <Panel title="Привал" tone="cloth">
          Костёр
        </Panel>
      </>,
    );

    expect(screen.getByRole("region", { name: "Партия" })).toHaveClass(
      "ui-panel",
      "ui-panel--wood",
    );
    expect(screen.getByRole("region", { name: "Привал" })).toHaveClass(
      "ui-panel--cloth",
    );
  });
});

describe("Badge", () => {
  test.each(["neutral", "amber", "cloth", "seal"] as const)(
    "тон %s",
    (tone) => {
      render(<Badge tone={tone}>Метка</Badge>);

      expect(screen.getByText("Метка")).toHaveClass(
        "ui-badge",
        `ui-badge--${tone}`,
      );
    },
  );
});

describe("Countdown", () => {
  test("таймер с понятным именем и временем", () => {
    render(<Countdown remainingSeconds={90} />);

    const timer = screen.getByRole("timer", { name: "Осталось 1:30" });
    expect(within(timer).getByText("1:30")).toBeInTheDocument();
  });

  test("подпись задаётся", () => {
    render(<Countdown remainingSeconds={5} label="До конца хода" />);

    expect(
      screen.getByRole("timer", { name: "До конца хода 0:05" }),
    ).toBeInTheDocument();
  });

  test("пауза: время стоит, свеча не мерцает", () => {
    const { container } = render(<Countdown remainingSeconds={42} paused />);

    expect(screen.getByText("Пауза")).toBeInTheDocument();
    expect(container.querySelector(".ui-anim-flicker")).toBeNull();
  });

  test("идёт: свеча мерцает", () => {
    const { container } = render(<Countdown remainingSeconds={42} />);

    expect(container.querySelector(".ui-anim-flicker")).not.toBeNull();
  });

  test("свеча тает пропорционально остатку", () => {
    const { container } = render(
      <Countdown remainingSeconds={30} totalSeconds={120} />,
    );

    expect(container.querySelector(".ui-countdown__wax > span")).toHaveStyle({
      width: "25%",
    });
  });

  test("остаток больше всего срока или срок 0 не ломают полосу", () => {
    const { container, rerender } = render(
      <Countdown remainingSeconds={200} totalSeconds={120} />,
    );
    expect(container.querySelector(".ui-countdown__wax > span")).toHaveStyle({
      width: "100%",
    });

    rerender(<Countdown remainingSeconds={5} totalSeconds={0} />);
    expect(container.querySelector(".ui-countdown__wax > span")).toHaveStyle({
      width: "0%",
    });
  });

  test("последние 10 секунд — тревожный тон; на паузе и дальше — нет", () => {
    const { rerender } = render(<Countdown remainingSeconds={10} />);
    expect(screen.getByRole("timer")).toHaveClass("ui-countdown--urgent");

    rerender(<Countdown remainingSeconds={11} />);
    expect(screen.getByRole("timer")).not.toHaveClass("ui-countdown--urgent");

    rerender(<Countdown remainingSeconds={3} paused />);
    expect(screen.getByRole("timer")).not.toHaveClass("ui-countdown--urgent");
  });
});

describe("SealedNote", () => {
  test("запечатанное письмо «Только тебе»", () => {
    render(<SealedNote>Стражник узнал твой герб.</SealedNote>);

    const note = screen.getByRole("note", { name: "Только тебе" });
    expect(within(note).getByText("Только тебе")).toBeInTheDocument();
    expect(
      within(note).getByText("Стражник узнал твой герб."),
    ).toBeInTheDocument();
  });
});

describe("HpBar", () => {
  test("шкала здоровья с числами для читалок", () => {
    render(<HpBar current={12} max={20} />);

    const meter = screen.getByRole("meter", { name: "Здоровье" });
    expect(meter).toHaveAttribute("aria-valuemin", "0");
    expect(meter).toHaveAttribute("aria-valuemax", "20");
    expect(meter).toHaveAttribute("aria-valuenow", "12");
    expect(meter).toHaveAttribute("aria-valuetext", "12 из 20");
    expect(within(meter).getByText("12 / 20")).toBeInTheDocument();
  });

  test("полоса пропорциональна, тон — по доле", () => {
    const { container, rerender } = render(<HpBar current={12} max={20} />);
    expect(container.querySelector(".ui-hp__fill")).toHaveStyle({
      width: "60%",
    });
    expect(screen.getByRole("meter")).toHaveClass("ui-hp--high");

    rerender(<HpBar current={8} max={20} />);
    expect(screen.getByRole("meter")).toHaveClass("ui-hp--mid");

    rerender(<HpBar current={3} max={20} />);
    expect(screen.getByRole("meter")).toHaveClass("ui-hp--low");
  });

  test("значения вне диапазона и max=0 не ломают шкалу", () => {
    const { container, rerender } = render(<HpBar current={25} max={20} />);
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "20");
    expect(container.querySelector(".ui-hp__fill")).toHaveStyle({
      width: "100%",
    });

    rerender(<HpBar current={-3} max={20} />);
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "0");
    expect(container.querySelector(".ui-hp__fill")).toHaveStyle({
      width: "0%",
    });

    rerender(<HpBar current={4} max={0} />);
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "0");
    expect(container.querySelector(".ui-hp__fill")).toHaveStyle({
      width: "0%",
    });
  });

  test("временные хиты — отдельная метка", () => {
    render(<HpBar current={12} max={20} temp={3} />);

    expect(screen.getByText("+3")).toBeInTheDocument();
    expect(screen.getByRole("meter")).toHaveAttribute(
      "aria-valuetext",
      "12 из 20, временных 3",
    );
  });

  test("подпись можно уточнить именем героя", () => {
    render(<HpBar current={1} max={9} label="Здоровье: Лира" />);

    expect(
      screen.getByRole("meter", { name: "Здоровье: Лира" }),
    ).toBeInTheDocument();
  });
});

describe("Avatar", () => {
  test("круглый жетон с инициалами и именем для читалок", () => {
    render(<Avatar name="Лира Ветер" />);

    const avatar = screen.getByRole("img", { name: "Лира Ветер" });
    expect(avatar).toHaveTextContent("ЛВ");
    expect(avatar).toHaveClass("ui-avatar", "ui-avatar--md");
  });

  test("размеры", () => {
    render(<Avatar name="Кай" size="lg" />);

    expect(screen.getByRole("img")).toHaveClass("ui-avatar--lg");
  });

  test("одно имя — один цвет", () => {
    render(
      <>
        <Avatar name="Борин" />
        <Avatar name="Борин" />
      </>,
    );

    const [first, second] = screen.getAllByRole("img");
    expect(first.className).toBe(second.className);
    expect(first.className).toMatch(/ui-avatar--tone-[0-5]/);
  });

  test("decorative — рядом уже есть имя текстом", () => {
    const { container } = render(<Avatar name="Лира" decorative />);

    expect(screen.queryByRole("img")).toBeNull();
    expect(container.firstElementChild).toHaveAttribute("aria-hidden", "true");
  });
});

describe("пустые состояния, загрузка, ошибка", () => {
  test("EmptyState: название — обычный абзац, действие рядом", () => {
    render(
      <EmptyState
        icon="scroll"
        title="Пока нет игр"
        action={<Button>Создать игру</Button>}
      >
        Создайте стол или войдите по коду.
      </EmptyState>,
    );

    expect(screen.getByText("Пока нет игр")).toBeInTheDocument();
    expect(screen.queryByRole("heading")).toBeNull();
    expect(
      screen.getByText("Создайте стол или войдите по коду."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Создать игру" }),
    ).toBeInTheDocument();
  });

  test("EmptyState: титул можно сделать заголовком", () => {
    render(<EmptyState title="Пусто" titleAs="h2" />);

    expect(
      screen.getByRole("heading", { level: 2, name: "Пусто" }),
    ).toBeInTheDocument();
  });

  test("Loading: статус с подписью", () => {
    const { rerender } = render(<Loading />);
    expect(screen.getByRole("status")).toHaveTextContent("Загрузка…");

    rerender(<Loading label="Входим…" />);
    expect(screen.getByRole("status")).toHaveTextContent("Входим…");
  });

  test("Alert: сообщение об ошибке — alert", () => {
    render(<Alert>Нет связи с сервером.</Alert>);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Нет связи с сервером.",
    );
    expect(screen.getByRole("alert")).toHaveClass("ui-alert");
  });
});

describe("prefers-reduced-motion", () => {
  const withAnimation: Array<[string, ReactElement]> = [
    ["Card appear", <Card appear>Текст</Card>],
    ["Countdown", <Countdown remainingSeconds={30} />],
    ["Loading", <Loading />],
  ];

  test.each(withAnimation)(
    "%s: движение разрешено — класс анимации есть",
    (_, element) => {
      stubReducedMotion(false);

      const { container } = render(element);

      expect(container.querySelector('[class*="ui-anim-"]')).not.toBeNull();
    },
  );

  test.each(withAnimation)(
    "%s: reduced motion — классов анимации нет",
    (_, element) => {
      stubReducedMotion(true);

      const { container } = render(element);

      expect(container.querySelector('[class*="ui-anim-"]')).toBeNull();
    },
  );
});
