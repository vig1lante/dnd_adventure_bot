import { render, screen, within } from "@testing-library/react";
import { partyMember } from "./fixtures";
import { PartyList } from "./PartyList";

const row = (name: string): HTMLElement => {
  const item = screen
    .getByText(name, { selector: ".party-member__name" })
    .closest("li");
  if (item === null) throw new Error(`Нет строки героя ${name}`);
  return item;
};

describe("отряд", () => {
  test("у каждого героя имя и шкала здоровья с числами сервера", () => {
    render(
      <PartyList
        userId="u1"
        party={[
          partyMember("pc_lira", "Лира", { hp: 7, hp_max: 10 }),
          partyMember("pc_boris", "Борин", { hp: 18, hp_max: 20 }),
        ]}
      />,
    );

    const lira = within(row("Лира")).getByRole("meter", {
      name: "Здоровье: Лира",
    });
    expect(lira).toHaveAttribute("aria-valuenow", "7");
    expect(lira).toHaveAttribute("aria-valuemax", "10");
    expect(
      within(row("Борин")).getByRole("meter", { name: "Здоровье: Борин" }),
    ).toHaveAttribute("aria-valuenow", "18");
  });

  test("временные хиты — отдельная метка", () => {
    render(
      <PartyList
        userId="u1"
        party={[partyMember("pc_lira", "Лира", { temp_hp: 3 })]}
      />,
    );

    expect(within(row("Лира")).getByText("+3")).toBeInTheDocument();
  });

  test("свой герой помечен «(вы)», чужие — нет", () => {
    render(
      <PartyList
        userId="u1"
        party={[
          partyMember("pc_lira", "Лира", { player_id: "u1" }),
          partyMember("pc_boris", "Борин", { player_id: "u2" }),
        ]}
      />,
    );

    expect(within(row("Лира")).getByText("(вы)")).toBeInTheDocument();
    expect(within(row("Борин")).queryByText("(вы)")).toBeNull();
  });

  test("состояния и эффекты названы, срок эффекта виден", () => {
    render(
      <PartyList
        userId="u1"
        party={[
          partyMember("pc_lira", "Лира", {
            conditions: [
              { id: "poisoned", name: "Отравлен", rounds_left: null },
            ],
            effects: [{ id: "bless", name: "Благословение", rounds_left: 3 }],
          }),
        ]}
      />,
    );

    const lira = within(row("Лира"));
    expect(lira.getByText("Отравлен")).toBeInTheDocument();
    expect(lira.getByText("Благословение (3 р.)")).toBeInTheDocument();
  });

  test("у героя без состояний лишних меток нет", () => {
    render(<PartyList userId="u1" party={[partyMember("pc_lira", "Лира")]} />);

    expect(row("Лира").querySelectorAll(".ui-badge")).toHaveLength(0);
  });

  test("без состояний и эффектов пустого блока меток нет", () => {
    render(<PartyList userId="u1" party={[partyMember("pc_lira", "Лира")]} />);

    expect(row("Лира").querySelector(".party-member__effects")).toBeNull();
  });

  test("отряда ещё нет — пояснение", () => {
    render(<PartyList userId="u1" party={[]} />);

    expect(screen.getByText("Отряд ещё не собран")).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).toBeNull();
  });

  test("имена и состояния выводятся как текст", () => {
    const { container } = render(
      <PartyList
        userId="u1"
        party={[
          partyMember("pc_lira", "<b>Лира</b>", {
            conditions: [
              {
                id: "x",
                name: "<img src=x onerror=alert(1)>",
                rounds_left: null,
              },
            ],
          }),
        ]}
      />,
    );

    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container).toHaveTextContent("<b>Лира</b>");
  });
});

describe("метки состояний и эффектов", () => {
  test.each([
    [
      "только состояние",
      {
        conditions: [{ id: "poisoned", name: "Отравлен", rounds_left: null }],
        effects: [],
      },
      "Отравлен",
    ],
    [
      "только эффект",
      {
        conditions: [],
        effects: [{ id: "bless", name: "Благословение", rounds_left: 3 }],
      },
      "Благословение (3 р.)",
    ],
  ])("%s показан без своей пары", (_name, overrides, text) => {
    render(
      <PartyList
        userId="u1"
        party={[partyMember("pc_lira", "Лира", overrides)]}
      />,
    );

    expect(within(row("Лира")).getByText(text)).toBeInTheDocument();
    expect(row("Лира").querySelectorAll(".ui-badge")).toHaveLength(1);
  });
});
