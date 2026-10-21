import { render, screen, within } from "@testing-library/react";
import type { CombatView } from "../api/types";
import { combatWith, heroFighter, monsterFighter } from "./fixtures";
import { ZoneView } from "./ZoneView";

const zoneNames = () =>
  screen.getAllByRole("heading").map((heading) => heading.textContent);
const fightersIn = (zoneName: string) => {
  const zone = screen
    .getByRole("heading", { name: zoneName })
    .closest("li") as HTMLElement;
  return within(zone)
    .queryAllByRole("listitem")
    .map((item) => item.querySelector(".zone__fighter-name")?.textContent);
};

/** Лира и Борин у входа, скелет и Фабиан в зале, на галерее пусто. */
function sampleCombat(): CombatView {
  return combatWith(
    "pc_lira",
    [
      heroFighter("pc_lira", "Лира", { zone_id: "gate" }),
      monsterFighter("cultist_fanatic-1", "Фабиан", { zone_id: "hall" }),
      heroFighter("pc_boris", "Борин", { zone_id: "gate" }),
      monsterFighter("skeleton-1", "Скелет", { zone_id: "hall" }),
    ],
    {
      zones: [
        { id: "gate", name: "У входа", combatants: ["pc_lira", "pc_boris"] },
        {
          id: "hall",
          name: "Главный зал",
          combatants: ["cultist_fanatic-1", "skeleton-1"],
        },
        { id: "gallery", name: "Галерея", combatants: [] },
      ],
    },
  );
}

describe("зоны боя", () => {
  test("зоны идут списком в порядке сервера, у каждой — название", () => {
    render(<ZoneView combat={sampleCombat()} />);

    expect(screen.getByRole("list", { name: "Зоны боя" })).toBeInTheDocument();
    expect(zoneNames()).toEqual(["У входа", "Главный зал", "Галерея"]);
  });

  test("в зоне перечислены её бойцы по именам из порядка хода", () => {
    render(<ZoneView combat={sampleCombat()} />);

    expect(fightersIn("У входа")).toEqual(["Лира", "Борин"]);
    // Имя NPC берётся из name бойца, а не из id или статблока.
    expect(fightersIn("Главный зал")).toEqual(["Фабиан", "Скелет"]);
    expect(screen.queryByText(/cultist_fanatic|skeleton-1/)).toBeNull();
  });

  test("враги помечены «Враг»", () => {
    render(<ZoneView combat={sampleCombat()} />);

    const hall = screen
      .getByRole("heading", { name: "Главный зал" })
      .closest("li") as HTMLElement;
    const gate = screen
      .getByRole("heading", { name: "У входа" })
      .closest("li") as HTMLElement;
    expect(within(hall).getAllByText("Враг")).toHaveLength(2);
    expect(within(gate).queryByText("Враг")).toBeNull();
  });

  test("пустая зона не выдаёт, что в ней кто-то спрятан", () => {
    render(<ZoneView combat={sampleCombat()} />);

    const gallery = screen
      .getByRole("heading", { name: "Галерея" })
      .closest("li") as HTMLElement;
    expect(within(gallery).getByText("Никого не видно")).toBeInTheDocument();
    expect(within(gallery).queryAllByRole("listitem")).toHaveLength(0);
  });

  test("боец, которого нет в порядке хода (спрятанный монстр), не показан и не назван", () => {
    const combat = sampleCombat();
    combat.zones[1].combatants.push("shade-1");

    render(<ZoneView combat={combat} />);

    expect(fightersIn("Главный зал")).toEqual(["Фабиан", "Скелет"]);
    expect(screen.queryByText(/shade/)).toBeNull();
  });

  test("боец стоит в зоне со своим zone_id, а не там, где его перечислили", () => {
    const combat = combatWith(
      "pc_lira",
      [
        heroFighter("pc_lira", "Лира", { zone_id: "hall" }),
        heroFighter("pc_boris", "Борин", { zone_id: "gate" }),
      ],
      {
        zones: [
          { id: "gate", name: "У входа", combatants: ["pc_lira"] },
          { id: "hall", name: "Главный зал", combatants: ["pc_boris"] },
        ],
      },
    );

    render(<ZoneView combat={combat} />);

    expect(fightersIn("У входа")).toEqual(["Борин"]);
    expect(fightersIn("Главный зал")).toEqual(["Лира"]);
  });

  test("боец с неизвестной зоной нигде не показывается", () => {
    const combat = combatWith("pc_lira", [
      heroFighter("pc_lira", "Лира", { zone_id: "cellar" }),
    ]);

    render(<ZoneView combat={combat} />);

    expect(screen.queryByText("Лира")).toBeNull();
    expect(screen.getByText("Никого не видно")).toBeInTheDocument();
  });

  test("бой без зон — пустой список без ошибок", () => {
    const combat = sampleCombat();
    combat.zones = [];

    render(<ZoneView combat={combat} />);

    expect(screen.queryByRole("heading")).toBeNull();
    expect(screen.getByText("Поле боя пока не описано")).toBeInTheDocument();
  });

  test("названия и имена выводятся только как текст", () => {
    const name = '<img src=x onerror="alert(1)">Склеп';
    const combat = combatWith("pc_lira", [
      heroFighter("pc_lira", "<b>Лира</b>", { zone_id: "z" }),
    ]);
    combat.zones = [{ id: "z", name, combatants: ["pc_lira"] }];

    const { container } = render(<ZoneView combat={combat} />);

    expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    expect(screen.getByText("<b>Лира</b>")).toBeInTheDocument();
    expect(container.querySelector("img, b")).toBeNull();
  });
});
