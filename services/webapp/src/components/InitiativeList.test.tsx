import { render, screen, within } from "@testing-library/react";
import type { CombatView } from "../api/types";
import { combatWith, heroFighter, monsterFighter } from "./fixtures";
import { InitiativeList } from "./InitiativeList";

const list = () => screen.getByRole("list", { name: "Порядок хода" });
const items = () => within(list()).getAllByRole("listitem");
const names = () =>
  items().map((item) => item.querySelector(".initiative__name")?.textContent);

/** Лира, скелет и Борин: порядок инициативы не совпадает с порядком в отряде. */
function sampleCombat(current: string | null = "pc_lira"): CombatView {
  return combatWith(current, [
    monsterFighter("skeleton-1", "Скелет"),
    heroFighter("pc_lira", "Лира", { hp: 9, hp_max: 10 }),
    heroFighter("pc_boris", "Борин"),
  ]);
}

describe("порядок хода", () => {
  test("бойцы идут в порядке инициативы, как прислал сервер", () => {
    render(<InitiativeList combat={sampleCombat()} />);

    expect(names()).toEqual(["Скелет", "Лира", "Борин"]);
  });

  test("текущий боец выделен: aria-current и метка «Ходит» только у него", () => {
    render(<InitiativeList combat={sampleCombat("pc_lira")} />);

    const [skeleton, lira, boris] = items();
    expect(lira).toHaveAttribute("aria-current", "true");
    expect(within(lira).getByText("Ходит")).toBeInTheDocument();
    for (const other of [skeleton, boris]) {
      expect(other).not.toHaveAttribute("aria-current");
      expect(within(other).queryByText("Ходит")).toBeNull();
    }
    expect(screen.getAllByText("Ходит")).toHaveLength(1);
  });

  test("ход монстра выделяет монстра", () => {
    render(<InitiativeList combat={sampleCombat("skeleton-1")} />);

    expect(items()[0]).toHaveAttribute("aria-current", "true");
    expect(items()[1]).not.toHaveAttribute("aria-current");
  });

  test("ходит спрятанный монстр (current_id пуст) — не выделен никто", () => {
    render(<InitiativeList combat={sampleCombat(null)} />);

    expect(screen.queryByText("Ходит")).toBeNull();
    for (const item of items()) {
      expect(item).not.toHaveAttribute("aria-current");
    }
  });

  test("current_id бойца, которого в списке нет, не выделяет никого и нигде не показан", () => {
    render(<InitiativeList combat={sampleCombat("shade-1")} />);

    expect(screen.queryByText("Ходит")).toBeNull();
    expect(screen.queryByText(/shade-1/)).toBeNull();
  });
});

describe("имена бойцов", () => {
  test("монстр, за которым стоит NPC, назван именем из name, а не статблока или id", () => {
    const combat = combatWith("cultist_fanatic-1", [
      heroFighter("pc_lira", "Лира"),
      monsterFighter("cultist_fanatic-1", "Фабиан"),
    ]);

    render(<InitiativeList combat={combat} />);

    expect(names()).toEqual(["Лира", "Фабиан"]);
    expect(screen.queryByText(/Фанатик культа/)).toBeNull();
    expect(screen.queryByText(/cultist_fanatic/)).toBeNull();
  });

  test("одинаковые монстры различаются номерами из name", () => {
    const combat = combatWith(null, [
      monsterFighter("goblin-1", "Гоблин 1"),
      monsterFighter("goblin-2", "Гоблин 2"),
    ]);

    render(<InitiativeList combat={combat} />);

    expect(names()).toEqual(["Гоблин 1", "Гоблин 2"]);
  });
});

describe("здоровье и состояния", () => {
  test("здоровье героя — шкала с числами сервера, клиент их не пересчитывает", () => {
    // Числа намеренно «неправильные»: шкала обязана показать ровно то, что пришло.
    const combat = combatWith("pc_lira", [
      heroFighter("pc_lira", "Лира", { hp: 3, hp_max: 27 }),
    ]);

    render(<InitiativeList combat={combat} />);

    const meter = screen.getByRole("meter", { name: "Здоровье: Лира" });
    expect(meter).toHaveAttribute("aria-valuenow", "3");
    expect(meter).toHaveAttribute("aria-valuemax", "27");
    expect(meter).toHaveTextContent("3 / 27");
  });

  test("у героя без чисел здоровья шкалы нет", () => {
    const combat = combatWith(null, [
      heroFighter("pc_lira", "Лира", { hp: null, hp_max: null }),
    ]);

    render(<InitiativeList combat={combat} />);

    expect(screen.queryByRole("meter")).toBeNull();
    expect(names()).toEqual(["Лира"]);
  });

  test.each(["цел", "ранен", "тяжело ранен", "повержен"] as const)(
    "монстр «%s» описан словами, без шкалы и чисел",
    (health) => {
      const combat = combatWith(null, [
        monsterFighter("skeleton-1", "Скелет", { health }),
      ]);

      render(<InitiativeList combat={combat} />);

      const item = items()[0];
      expect(within(item).getByText(health)).toBeInTheDocument();
      expect(within(item).queryByRole("meter")).toBeNull();
      expect(item.textContent).not.toMatch(/\d/);
    },
  );

  test("неизвестное слово о здоровье монстра показывается как есть", () => {
    const combat = combatWith(null, [
      monsterFighter("skeleton-1", "Скелет", {
        health: "при смерти" as never,
      }),
    ]);

    render(<InitiativeList combat={combat} />);

    expect(within(items()[0]).getByText("при смерти")).toBeInTheDocument();
  });

  test("враги помечены «Враг», отряд — нет", () => {
    render(<InitiativeList combat={sampleCombat()} />);

    const [skeleton, lira, boris] = items();
    expect(within(skeleton).getByText("Враг")).toBeInTheDocument();
    expect(within(lira).queryByText("Враг")).toBeNull();
    expect(within(boris).queryByText("Враг")).toBeNull();
  });

  test("состояния бойца — плашки с названием и сроком от сервера", () => {
    const combat = combatWith(null, [
      heroFighter("pc_lira", "Лира", {
        conditions: [
          { id: "poisoned", name: "Отравлен", rounds_left: 2 },
          { id: "prone", name: "Сбит с ног", rounds_left: null },
        ],
      }),
      monsterFighter("skeleton-1", "Скелет", {
        conditions: [{ id: "frightened", name: "Напуган", rounds_left: 1 }],
      }),
    ]);

    render(<InitiativeList combat={combat} />);

    const [lira, skeleton] = items();
    expect(within(lira).getByText("Отравлен (2 р.)")).toBeInTheDocument();
    expect(within(lira).getByText("Сбит с ног")).toBeInTheDocument();
    expect(within(skeleton).getByText("Напуган (1 р.)")).toBeInTheDocument();
  });
});

describe("герой, который не может действовать", () => {
  const paralyzed = { id: "paralyzed", name: "Парализован", rounds_left: 2 };

  /** Лира парализована (can_act: false), Борин здоров, Скелет — монстр; Лира ходит. */
  function disabledCombat(): CombatView {
    return combatWith("pc_lira", [
      heroFighter("pc_lira", "Лира", {
        can_act: false,
        conditions: [paralyzed],
      }),
      heroFighter("pc_boris", "Борин", { can_act: true }),
      monsterFighter("skeleton-1", "Скелет"),
    ]);
  }

  test("у такого героя пометка «Не может действовать», у остальных её нет", () => {
    render(<InitiativeList combat={disabledCombat()} />);

    const [lira, boris, skeleton] = items();
    expect(within(lira).getByText("Не может действовать")).toBeInTheDocument();
    expect(within(boris).queryByText("Не может действовать")).toBeNull();
    expect(within(skeleton).queryByText("Не может действовать")).toBeNull();
    expect(screen.getAllByText("Не может действовать")).toHaveLength(1);
  });

  test("пометка стоит рядом с «Ходит» и состоянием, ничего не заменяя", () => {
    render(<InitiativeList combat={disabledCombat()} />);

    const lira = items()[0];
    expect(lira).toHaveAttribute("aria-current", "true");
    expect(within(lira).getByText("Ходит")).toBeInTheDocument();
    expect(within(lira).getByText("Парализован (2 р.)")).toBeInTheDocument();
    expect(within(lira).getByRole("meter")).toBeInTheDocument();
  });

  test("помечен и тот, чья очередь ещё не пришла", () => {
    const combat = combatWith("skeleton-1", [
      monsterFighter("skeleton-1", "Скелет"),
      heroFighter("pc_lira", "Лира", { can_act: false }),
    ]);

    render(<InitiativeList combat={combat} />);

    expect(
      within(items()[1]).getByText("Не может действовать"),
    ).toBeInTheDocument();
    expect(within(items()[0]).getByText("Ходит")).toBeInTheDocument();
  });

  test("can_act: true и герой без признака (старый сервер) не помечаются", () => {
    const combat = combatWith("pc_lira", [
      heroFighter("pc_lira", "Лира", { can_act: true }),
      heroFighter("pc_boris", "Борин"),
    ]);

    render(<InitiativeList combat={combat} />);

    expect(screen.queryByText("Не может действовать")).toBeNull();
  });

  test("монстр без can_act не помечается, даже поверженный", () => {
    const combat = combatWith(null, [
      monsterFighter("skeleton-1", "Скелет", { health: "повержен" }),
    ]);

    render(<InitiativeList combat={combat} />);

    expect(screen.queryByText("Не может действовать")).toBeNull();
  });
});

describe("тексты — только текст", () => {
  test("имя с разметкой остаётся буквами", () => {
    const name = '<img src=x onerror="alert(1)"><b>Тень</b>';
    const combat = combatWith("skeleton-1", [
      monsterFighter("skeleton-1", name, {
        conditions: [{ id: "x", name: "<script>x()</script>", rounds_left: 1 }],
      }),
    ]);

    const { container } = render(<InitiativeList combat={combat} />);

    expect(screen.getByText(name)).toBeInTheDocument();
    expect(container.querySelector("img, b, script")).toBeNull();
    expect(screen.getByText("<script>x()</script> (1 р.)")).toBeInTheDocument();
  });
});

describe("движение", () => {
  const reduceMotion = () =>
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("prefers-reduced-motion"),
      addEventListener: () => {},
      removeEventListener: () => {},
    }));

  test("метка «Ходит» появляется мягко", () => {
    const { container } = render(<InitiativeList combat={sampleCombat()} />);

    expect(container.querySelectorAll(".ui-anim-appear")).toHaveLength(1);
  });

  test("при prefers-reduced-motion анимаций нет", () => {
    reduceMotion();

    const { container } = render(<InitiativeList combat={sampleCombat()} />);

    expect(container.querySelector("[class*='ui-anim-']")).toBeNull();
    expect(screen.getByText("Ходит")).toBeInTheDocument();
  });
});

describe("подсветка текущего хода", () => {
  test("класс подсветки стоит только у бойца, чей сейчас ход", () => {
    render(<InitiativeList combat={sampleCombat("pc_lira")} />);

    const [skeleton, lira, boris] = items();
    expect(lira).toHaveClass("initiative__item--current");
    expect(skeleton).not.toHaveClass("initiative__item--current");
    expect(boris).not.toHaveClass("initiative__item--current");
  });

  test("ход перешёл — подсветка перешла вместе с ним", () => {
    const { rerender } = render(
      <InitiativeList combat={sampleCombat("pc_lira")} />,
    );

    rerender(<InitiativeList combat={sampleCombat("pc_boris")} />);

    const [skeleton, lira, boris] = items();
    expect(boris).toHaveClass("initiative__item--current");
    expect(lira).not.toHaveClass("initiative__item--current");
    expect(skeleton).not.toHaveClass("initiative__item--current");
  });
});

describe("герой с неполными числами здоровья", () => {
  test.each([
    ["нет максимума", { hp: 5, hp_max: null }],
    ["нет текущего", { hp: null, hp_max: 10 }],
  ])("%s: полоски нет, экран не ломается", (_name, patch) => {
    const combat = combatWith("pc_lira", [
      heroFighter("pc_lira", "Лира", patch),
      monsterFighter("skeleton-1", "Скелет"),
    ]);

    render(<InitiativeList combat={combat} />);

    expect(names()).toEqual(["Лира", "Скелет"]);
    expect(screen.queryByRole("meter")).toBeNull();
  });

  test("без состояний пустого блока эффектов нет", () => {
    const { container } = render(<InitiativeList combat={sampleCombat()} />);

    expect(container.querySelector(".initiative__effects")).toBeNull();
  });
});

describe("герой без чисел здоровья", () => {
  test("в порядке хода он есть, полоски нет, экран не ломается", () => {
    const combat = combatWith("pc_lira", [
      heroFighter("pc_lira", "Лира", { hp: null, hp_max: null }),
      monsterFighter("skeleton-1", "Скелет"),
    ]);

    render(<InitiativeList combat={combat} />);

    expect(names()).toEqual(["Лира", "Скелет"]);
    expect(screen.queryByRole("meter")).toBeNull();
  });
});
